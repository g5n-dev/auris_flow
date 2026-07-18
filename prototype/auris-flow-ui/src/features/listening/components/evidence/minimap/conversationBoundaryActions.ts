import { saveConversationBoundary } from "../../../../../api/client";
import { clamp } from "../../../../../shared/runtime/math";
import { boundaryExtensionCandidates } from "../../../fixtures/boundaryFixtures";
import type { StitchedWavSlice } from "../../../fixtures/boundaryFixtures";
import type { AnnotationMinimapProps } from "./annotationMinimapTypes";
import type { BoundaryExtensionActions } from "./boundaryExtensionActions";
import type { PointerEvent as ReactPointerEvent } from "react";

export function createConversationBoundaryActions(context: BoundaryExtensionActions) {
  const { activeEdit, activeEvent, boundaryAxisDuration, boundaryAxisEnd, boundaryAxisStart, boundaryDrag, boundaryDragAxisRef, boundaryDragVisibleExtensionIdsRef, conversationDuration, employeeDragClickGuard, extensionDrafts, markSyncDirty, modalBoundaryStripRef, sessionBoundary, sessionClockAt, sessionRangeText, setAssociationEdits, setBoundaryConfirmed, setBoundaryDrag, setBoundaryPreview, setDragOverEmployee, setDraggedEmployee, setEmployeeOrder, setExtensionDrafts, setExtensionLocks, setSelectedExtensionId, setSelectedWindow, setSessionBoundary, setSliceDecisions, setSyncState, sliceDecisions, stitchStripRef, syncBoundaryExtensionOverlap, syncState, visibleExtensionRanges } = context;
  const updateSessionBoundaryValue = (edge: "start" | "end", value: number) => {
      if (Number.isNaN(value)) return;
      const minWindowSeconds = 30;
      markSyncDirty();
      setSessionBoundary((current) => {
        if (edge === "start") {
          const nextStart = Number(clamp(value, boundaryAxisStart, current.end - minWindowSeconds).toFixed(1));
          syncBoundaryExtensionOverlap(edge, nextStart);
          return { ...current, start: nextStart };
        }
        const nextEnd = Number(clamp(value, current.start + minWindowSeconds, boundaryAxisEnd).toFixed(1));
        syncBoundaryExtensionOverlap(edge, nextEnd);
        return { ...current, end: nextEnd };
      });
    };

  const secondsFromBoundaryPointer = (clientX: number, rect: DOMRect) => {
      const dragAxis = boundaryDragAxisRef.current;
      const axisStart = dragAxis?.start ?? boundaryAxisStart;
      const axisDuration = dragAxis?.duration ?? boundaryAxisDuration;
      return Number((axisStart + clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1) * axisDuration).toFixed(1));
    };

  const updateBoundaryFromRect = (clientX: number, edge: "start" | "end", rect: DOMRect) => {
      updateSessionBoundaryValue(edge, secondsFromBoundaryPointer(clientX, rect));
    };

  const nudgeSessionBoundary = (edge: "start" | "end", deltaSeconds: number) => {
      const base = edge === "start" ? sessionBoundary.start : sessionBoundary.end;
      updateSessionBoundaryValue(edge, base + deltaSeconds);
    };

  const resetConversationBoundary = () => {
      setSessionBoundary({ start: 0, end: conversationDuration });
      setExtensionDrafts(Object.fromEntries(boundaryExtensionCandidates.map((candidate) => [candidate.id, "idle"])));
      setExtensionLocks(
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
      setSelectedExtensionId(null);
      setBoundaryPreview(null);
      markSyncDirty();
    };

  const clearEmployeeDragState = () => {
      setDraggedEmployee(null);
      setDragOverEmployee(null);
    };

  const releaseEmployeeDragClickGuard = () => {
      window.setTimeout(() => {
        employeeDragClickGuard.current = false;
      }, 160);
    };

  const moveEmployeeLane = (sourceSub: string, targetSub: string) => {
      setEmployeeOrder((current) => {
        const sourceIndex = current.indexOf(sourceSub);
        const targetIndex = current.indexOf(targetSub);
        if (sourceIndex < 0 || targetIndex < 0 || sourceSub === targetSub) return current;
        const next = [...current];
        const [moved] = next.splice(sourceIndex, 1);
        next.splice(targetIndex, 0, moved);
        return next;
      });
    };

  const updateActiveAssociation = (patch: Partial<{ targetId: string; confidence: number; status: string }>) => {
      setAssociationEdits((current) => ({
        ...current,
        [activeEvent.id]: {
          targetId: activeEdit.targetId,
          confidence: activeEdit.confidence,
          status: activeEdit.status,
          ...patch
        }
      }));
    };

  const updateBoundaryFromPointer = (clientX: number, edge: "start" | "end") => {
      const rect = stitchStripRef.current?.getBoundingClientRect();
      if (!rect) return;
      updateBoundaryFromRect(clientX, edge, rect);
    };

  const nudgeBoundary = (edge: "start" | "end", deltaSeconds: number) => {
      const base = edge === "start" ? sessionBoundary.start : sessionBoundary.end;
      updateSessionBoundaryValue(edge, base + deltaSeconds);
    };

  const startBoundaryDrag = (event: ReactPointerEvent<HTMLElement>, edge: "start" | "end") => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      stitchStripRef.current?.setPointerCapture?.(event.pointerId);
      boundaryDragVisibleExtensionIdsRef.current = new Set(visibleExtensionRanges.map((candidate) => candidate.id));
      boundaryDragAxisRef.current = { start: boundaryAxisStart, end: boundaryAxisEnd, duration: boundaryAxisDuration };
      setBoundaryDrag(edge);
      updateBoundaryFromPointer(event.clientX, edge);
    };

  const finishBoundaryDrag = (event: ReactPointerEvent<HTMLElement>) => {
      if (!boundaryDrag) {
        boundaryDragVisibleExtensionIdsRef.current = null;
        boundaryDragAxisRef.current = null;
        return;
      }
      if (stitchStripRef.current?.hasPointerCapture?.(event.pointerId)) {
        stitchStripRef.current.releasePointerCapture(event.pointerId);
      }
      if (modalBoundaryStripRef.current?.hasPointerCapture?.(event.pointerId)) {
        modalBoundaryStripRef.current.releasePointerCapture(event.pointerId);
      }
      boundaryDragVisibleExtensionIdsRef.current = null;
      boundaryDragAxisRef.current = null;
      setBoundaryDrag(null);
      setSelectedWindow(sessionRangeText.slice(0, 5) + " - " + sessionRangeText.slice(11, 16));
    };

  const updateModalBoundaryFromPointer = (clientX: number, edge: "start" | "end") => {
      const rect = modalBoundaryStripRef.current?.getBoundingClientRect();
      if (!rect) return;
      updateBoundaryFromRect(clientX, edge, rect);
    };

  const startModalBoundaryDrag = (event: ReactPointerEvent<HTMLElement>, edge: "start" | "end") => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      modalBoundaryStripRef.current?.setPointerCapture?.(event.pointerId);
      boundaryDragVisibleExtensionIdsRef.current = new Set(visibleExtensionRanges.map((candidate) => candidate.id));
      boundaryDragAxisRef.current = { start: boundaryAxisStart, end: boundaryAxisEnd, duration: boundaryAxisDuration };
      setBoundaryDrag(edge);
      updateModalBoundaryFromPointer(event.clientX, edge);
    };

  const moveNearestModalBoundary = (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      const rect = modalBoundaryStripRef.current?.getBoundingClientRect();
      if (!rect) return;
      const nextSeconds = secondsFromBoundaryPointer(event.clientX, rect);
      const edge = Math.abs(nextSeconds - sessionBoundary.start) <= Math.abs(nextSeconds - sessionBoundary.end) ? "start" : "end";
      updateSessionBoundaryValue(edge, nextSeconds);
    };

  const setSliceDecision = (sliceId: string, decision: "merged" | "split") => {
      setSliceDecisions((current) => ({ ...current, [sliceId]: decision }));
      markSyncDirty();
    };

  const markBoundaryAtSlice = (slice: StitchedWavSlice, edge: "start" | "end") => {
      setSessionBoundary((current) => {
        if (edge === "start") return { ...current, start: Math.min(slice.conversationStart, current.end - 30) };
        return { ...current, end: Math.max(slice.conversationEnd, current.start + 30) };
      });
      markSyncDirty();
      setSelectedWindow(`${slice.wallStart.slice(0, 5)} - ${slice.wallEnd.slice(0, 5)}`);
    };

  const confirmSessionBoundary = async () => {
      if (syncState === "saving") return;
      setSyncState("saving");
      try {
        const response = await saveConversationBoundary("boundary_s128_v1", {
          audio_session_id: "S20250526-000128",
          start_ms: Math.max(0, Math.round(sessionBoundary.start * 1000)),
          end_ms: Math.max(0, Math.round(sessionBoundary.end * 1000)),
          decision: "manual_confirmed",
          merged_slice_ids: Object.entries(sliceDecisions)
            .filter(([, decision]) => decision === "merged")
            .map(([sliceId]) => sliceId),
          split_slice_ids: Object.entries(sliceDecisions)
            .filter(([, decision]) => decision === "split")
            .map(([sliceId]) => sliceId),
          extension_ids: Object.entries(extensionDrafts)
            .filter(([, decision]) => decision === "merged")
            .map(([candidateId]) => candidateId)
        });
        setBoundaryConfirmed(true);
        setSyncState("synced");
        setSelectedWindow(`${sessionClockAt(sessionBoundary.start).slice(0, 5)} - ${sessionClockAt(sessionBoundary.end).slice(0, 5)}`);
        setBoundaryPreview({
          kind: "slice",
          id: "boundary_s128_v1",
          clip: "source",
          label: "会话边界确认",
          windowText: `Trace ${response.meta?.trace_id ?? response.data.trace_id ?? "pending"}`,
          playing: false
        });
      } catch {
        setBoundaryConfirmed(false);
        setSyncState("error");
      }
    };

  return { ...context, updateSessionBoundaryValue, secondsFromBoundaryPointer, updateBoundaryFromRect, nudgeSessionBoundary, resetConversationBoundary, clearEmployeeDragState, releaseEmployeeDragClickGuard, moveEmployeeLane, updateActiveAssociation, updateBoundaryFromPointer, nudgeBoundary, startBoundaryDrag, finishBoundaryDrag, updateModalBoundaryFromPointer, startModalBoundaryDrag, moveNearestModalBoundary, setSliceDecision, markBoundaryAtSlice, confirmSessionBoundary };
}

export type AnnotationMinimapController = ReturnType<typeof createConversationBoundaryActions>;
