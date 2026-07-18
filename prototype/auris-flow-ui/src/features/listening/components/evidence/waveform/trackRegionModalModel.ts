import { clamp } from "../../../../../shared/runtime/math";
import { stitchedWavSlices } from "../../../fixtures/boundaryFixtures";
import { clockToSeconds, secondsToClock } from "../../../model/listeningTime";
import { layoutOverlappingRegions, percentToClock, trackHeightFor } from "../../../model/trackLayout";
import type { TrackRegion } from "../../../model/trackLayout";
import type { TrackRegionActions } from "./trackRegionActions";
import type { WaveformPanelProps } from "./waveformPanelTypes";

export function buildTrackRegionModalModel(context: TrackRegionActions) {
  const { allTracks, hiddenTracks, regionsForTrackWithEdits, selectedRegion, trackRegionModalId } = context;
  const trackLayouts = allTracks.filter((track) => !hiddenTracks[track.key]).map((track) => {
      const laidRegions = layoutOverlappingRegions(regionsForTrackWithEdits(track.key));
      const laneCount = laidRegions[0]?.laneCount ?? 1;
      return {
        track,
        regions: laidRegions,
        height: trackHeightFor(laneCount)
      };
    });

  const regionContexts = trackLayouts.flatMap((layout) => layout.regions.map((region) => ({ track: layout.track, region })));

  const selectedRegionContext = regionContexts.find((item) => item.region.id === selectedRegion);

  const selectedRegionData = selectedRegionContext?.region;

  const modalRegionContext = trackRegionModalId ? regionContexts.find((item) => item.region.id === trackRegionModalId) : undefined;

  const modalRegion = modalRegionContext?.region;

  const modalTrack = modalRegionContext?.track;

  const modalSourceSlice = modalRegion
      ? stitchedWavSlices.find((slice) => {
          const regionStartSeconds = clockToSeconds(percentToClock(modalRegion.left));
          return regionStartSeconds >= clockToSeconds(slice.wallStart) && regionStartSeconds <= clockToSeconds(slice.wallEnd);
        }) ?? stitchedWavSlices[1]
      : null;

  const modalRegionTime = modalRegion ? `${percentToClock(modalRegion.left)} - ${percentToClock(modalRegion.left + modalRegion.width)}` : "";

  const modalRegionEnd = modalRegion ? Number(clamp(modalRegion.left + modalRegion.width, 2, 100).toFixed(2)) : 0;

  const inferRegionFieldKey = (trackKey: string, label: string) => {
      const normalized = label
        .replace(/[^\u4e00-\u9fa5a-zA-Z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .toLowerCase();
      const known: Record<string, string> = {
        "金额冲突": "qa.amount_conflict",
        "报价": "intent.quote",
        "报价承诺": "intent.quote_commitment",
        "价格异议": "intent.price_objection",
        "优惠:3.5万": "entity.discount_amount",
        "落地:28.19万": "entity.final_price",
        "指导价:31.69万": "entity.guide_price",
        "VAD 有声": "audio.vad_voice",
        "销售A": "speaker.sales_a",
        "客户": "speaker.customer"
      };
      return known[label] ?? `${trackKey}.${normalized || "custom_tag"}`;
    };

  const inferRegionValue = (trackKey: string, label: string) => {
      if (label.includes(":")) return label.split(":").slice(1).join(":");
      if (trackKey === "vad") return "voice_active=true";
      if (trackKey === "speaker") return label;
      if (trackKey === "qa") return label.includes("冲突") ? "需要人工复核" : "待确认";
      if (trackKey === "doc") return "business_event_linked";
      return label;
    };

  const modalRegionDraft = modalRegion && modalTrack
      ? {
          value: modalRegion.value ?? inferRegionValue(modalTrack.key, modalRegion.label),
          fieldKey: modalRegion.fieldKey ?? inferRegionFieldKey(modalTrack.key, modalRegion.label),
          confidence: modalRegion.confidence ?? (modalRegion.tone.includes("er") ? 82 : modalTrack.key === "vad" ? 97 : 91),
          reviewState: modalRegion.reviewState ?? (modalRegion.tone.includes("er") ? "待人工复核" : "AI建议"),
          evidenceRef: modalRegion.evidenceRef ?? `${modalSourceSlice?.label ?? "W2"} / ASR-${modalRegion.id}`,
          writeTarget: modalRegion.writeTarget ?? "auris/label/segment_annotations",
          sourceText:
            modalRegion.sourceText ??
            (modalTrack.key === "vad"
              ? "VAD 连续有声。"
              : modalTrack.key === "qa"
                ? "报价金额与单据不一致。"
                : "基于片段、ASR 和单据生成建议。"),
          note: modalRegion.note ?? "",
          assignee: modalRegion.assignee ?? "当前登录用户"
        }
      : null;

  const previewWindowForRegion = (region: TrackRegion, clip: "before" | "current" | "after") => {
      const startSeconds = clockToSeconds(percentToClock(region.left));
      const endSeconds = clockToSeconds(percentToClock(region.left + region.width));
      if (clip === "before") return `${secondsToClock(Math.max(0, startSeconds - 5))} - ${secondsToClock(startSeconds)}`;
      if (clip === "after") return `${secondsToClock(endSeconds)} - ${secondsToClock(endSeconds + 5)}`;
      return `${secondsToClock(startSeconds)} - ${secondsToClock(endSeconds)}`;
    };

  return { ...context, trackLayouts, regionContexts, selectedRegionContext, selectedRegionData, modalRegionContext, modalRegion, modalTrack, modalSourceSlice, modalRegionTime, modalRegionEnd, inferRegionFieldKey, inferRegionValue, modalRegionDraft, previewWindowForRegion };
}

export type TrackRegionModalModel = ReturnType<typeof buildTrackRegionModalModel>;
