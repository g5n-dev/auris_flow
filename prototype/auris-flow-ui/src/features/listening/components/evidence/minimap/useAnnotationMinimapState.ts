import { eventLinks } from "../../../../../shared/fixtures/eventLinks";
import { boundaryExtensionCandidates, stitchedWavSlices } from "../../../fixtures/boundaryFixtures";
import type { BoundaryExtensionDecision, BoundaryExtensionLock, BoundaryPreviewState } from "../../../fixtures/boundaryFixtures";
import { lanes } from "../../../fixtures/evidenceFixtures";
import type { AnnotationMinimapProps } from "./annotationMinimapTypes";
import { useRef, useState } from "react";

export function useAnnotationMinimapState(props: AnnotationMinimapProps) {
  const {
  audioSessionId,
  boundaryId,
  onReviewChange,
  selectedWindow,
  setSelectedWindow,
  activeTrack,
  setActiveTrack,
  hiddenTracks,
  setHiddenTracks,
  openListeningMode
} = props;
  const [mode, setMode] = useState<"day" | "segmented" | "receipt">("receipt");

  const [collapsed, setCollapsed] = useState(false);

  const [employeeOrder, setEmployeeOrder] = useState(lanes.map((lane) => lane.sub));

  const [draggedEmployee, setDraggedEmployee] = useState<string | null>(null);

  const [dragOverEmployee, setDragOverEmployee] = useState<string | null>(null);

  const [selectedSliceId, setSelectedSliceId] = useState<string | null>(null);

  const [sliceDecisions, setSliceDecisions] = useState<Record<string, "merged" | "split">>(
      Object.fromEntries(stitchedWavSlices.map((slice) => [slice.id, "merged"]))
    );

  const [extensionDrafts, setExtensionDrafts] = useState<Record<string, BoundaryExtensionDecision>>(
      Object.fromEntries(boundaryExtensionCandidates.map((candidate) => [candidate.id, "idle"]))
    );

  const [extensionSourceExpanded, setExtensionSourceExpanded] = useState(false);

  const [selectedExtensionId, setSelectedExtensionId] = useState<string | null>(null);

  const [extensionLocks, setExtensionLocks] = useState<Record<string, BoundaryExtensionLock>>(
      Object.fromEntries(
        boundaryExtensionCandidates.map((candidate) => [
          candidate.id,
          {
            startClock: candidate.previewStart,
            endClock: candidate.previewEnd
          }
        ])
      )
    );

  const [boundaryPreview, setBoundaryPreview] = useState<BoundaryPreviewState | null>(null);

  const [syncState, setSyncState] = useState<"synced" | "dirty" | "saving" | "error">("synced");

  const [sessionBoundary, setSessionBoundary] = useState({ start: 0, end: stitchedWavSlices[stitchedWavSlices.length - 1]?.conversationEnd ?? 0 });

  const [boundaryDrag, setBoundaryDrag] = useState<"start" | "end" | null>(null);

  const [extensionDrag, setExtensionDrag] = useState<{ candidateId: string; mode: "start" | "end" | "window" } | null>(null);

  const [boundaryConfirmed, setBoundaryConfirmed] = useState(false);

  const employeeDragClickGuard = useRef(false);

  const stitchStripRef = useRef<HTMLDivElement>(null);

  const modalBoundaryStripRef = useRef<HTMLDivElement>(null);

  const extensionDragRef = useRef<{
      candidateId: string;
      mode: "start" | "end" | "window";
      pointerStart: number;
      lockStart: number;
      lockEnd: number;
    } | null>(null);

  const boundaryDragVisibleExtensionIdsRef = useRef<Set<string> | null>(null);

  const boundaryDragAxisRef = useRef<{ start: number; end: number; duration: number } | null>(null);

  const [visibleEmployees, setVisibleEmployees] = useState<Record<string, boolean>>(
      Object.fromEntries(lanes.map((lane) => [lane.sub, true]))
    );

  const [layers, setLayers] = useState<Record<string, boolean>>({
      energy: true,
      sop: true,
      overlap: true,
      biz: true,
      orphan: false
    });

  const [eventAssociationsImported, setEventAssociationsImported] = useState(true);

  const [activeAssociation, setActiveAssociation] = useState(eventLinks[1].id);

  const [associationEdits, setAssociationEdits] = useState<Record<string, { targetId: string; confidence: number; status: string }>>(
      Object.fromEntries(
        eventLinks.map((event) => [
          event.id,
          {
            targetId: event.id,
            confidence: event.confidence,
            status: "AI建议"
          }
        ])
      )
    );

  return { audioSessionId, boundaryId, onReviewChange, selectedWindow, setSelectedWindow, activeTrack, setActiveTrack, hiddenTracks, setHiddenTracks, openListeningMode, mode, setMode, collapsed, setCollapsed, employeeOrder, setEmployeeOrder, draggedEmployee, setDraggedEmployee, dragOverEmployee, setDragOverEmployee, selectedSliceId, setSelectedSliceId, sliceDecisions, setSliceDecisions, extensionDrafts, setExtensionDrafts, extensionSourceExpanded, setExtensionSourceExpanded, selectedExtensionId, setSelectedExtensionId, extensionLocks, setExtensionLocks, boundaryPreview, setBoundaryPreview, syncState, setSyncState, sessionBoundary, setSessionBoundary, boundaryDrag, setBoundaryDrag, extensionDrag, setExtensionDrag, boundaryConfirmed, setBoundaryConfirmed, employeeDragClickGuard, stitchStripRef, modalBoundaryStripRef, extensionDragRef, boundaryDragVisibleExtensionIdsRef, boundaryDragAxisRef, visibleEmployees, setVisibleEmployees, layers, setLayers, eventAssociationsImported, setEventAssociationsImported, activeAssociation, setActiveAssociation, associationEdits, setAssociationEdits };
}

export type AnnotationMinimapState = ReturnType<typeof useAnnotationMinimapState>;
