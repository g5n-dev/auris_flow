import { eventLinks } from "../../../../../shared/fixtures/eventLinks";
import type { LabelTrackKey } from "../../../../../shared/fixtures/labelLayers";
import { clamp } from "../../../../../shared/runtime/math";
import { eventTrackBindings, labelTrackMeta, lanes, minimapTrackFilterKeys, minimapVoiceAssociationKeys } from "../../../fixtures/evidenceFixtures";
import type { AnnotationMinimapProps } from "./annotationMinimapTypes";
import type { AnnotationMinimapState } from "./useAnnotationMinimapState";

export function buildAnnotationAssociationModel(context: AnnotationMinimapState) {
  const { activeAssociation, activeTrack, associationEdits, employeeOrder, eventAssociationsImported, hiddenTracks, layers, mode } = context;
  const associationTargets = eventLinks.map((event, index) => ({
      ...event,
      targetLeft: clamp(event.left + (index % 2 === 0 ? 1.6 : -1.4), 0, 90),
      targetWidth: Math.max(7, event.width)
    }));

  const activeTrackKey = labelTrackMeta.some((track) => track.key === activeTrack) ? (activeTrack as LabelTrackKey) : null;

  const activeTrackFiltersEvents = activeTrackKey ? minimapTrackFilterKeys.includes(activeTrackKey) : false;

  const eventIsVisibleByTrack = (event: (typeof eventLinks)[number]) => {
      const boundTracks = eventTrackBindings[event.id] ?? ["doc"];
      if (!boundTracks.some((track) => !hiddenTracks[track])) return false;
      return activeTrackFiltersEvents && activeTrackKey ? boundTracks.includes(activeTrackKey) : true;
    };

  const minimapFilteredEvents = eventLinks.filter(eventIsVisibleByTrack);

  const visibleAssociationTargets = associationTargets.filter((target) => minimapFilteredEvents.some((event) => event.id === target.id));

  const showVoiceAssociations = minimapVoiceAssociationKeys.some((track) => !hiddenTracks[track]);

  const showDocAssociations = !hiddenTracks.doc;

  const showEnergyLayer = layers.energy && !hiddenTracks.vad;

  const showOverlapLayer = layers.overlap && !hiddenTracks.cross;

  const showBizLayer = layers.biz && minimapTrackFilterKeys.some((track) => !hiddenTracks[track]);

  const showAssociationLayer = eventAssociationsImported && mode === "receipt" && minimapFilteredEvents.length > 0 && (showVoiceAssociations || showDocAssociations);

  const activeEvent = minimapFilteredEvents.find((event) => event.id === activeAssociation) ?? minimapFilteredEvents[0] ?? eventLinks[0];

  const activeEdit = associationEdits[activeEvent.id] ?? {
      targetId: activeEvent.id,
      confidence: activeEvent.confidence,
      status: "AI建议"
    };

  const activeTarget = visibleAssociationTargets.find((target) => target.id === activeEdit.targetId);

  const confidencePct = Math.round(activeEdit.confidence * 100);

  const orderedLanes = employeeOrder
      .map((sub) => lanes.find((lane) => lane.sub === sub))
      .filter((lane): lane is (typeof lanes)[number] => Boolean(lane));

  return { ...context, associationTargets, activeTrackKey, activeTrackFiltersEvents, eventIsVisibleByTrack, minimapFilteredEvents, visibleAssociationTargets, showVoiceAssociations, showDocAssociations, showEnergyLayer, showOverlapLayer, showBizLayer, showAssociationLayer, activeEvent, activeEdit, activeTarget, confidencePct, orderedLanes };
}

export type AnnotationAssociationModel = ReturnType<typeof buildAnnotationAssociationModel>;
