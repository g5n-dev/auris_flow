import { clamp } from "../../../../../shared/runtime/math";
import type { BoundaryExtensionCandidate, BoundaryExtensionDecision, BoundaryPreviewClip } from "../../../fixtures/boundaryFixtures";
import type { AnnotationBoundaryModel } from "./annotationBoundaryModel";
import type { AnnotationMinimapProps, ExtensionRange } from "./annotationMinimapTypes";
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from "react";

export function createBoundaryExtensionActions(context: AnnotationBoundaryModel) {
  const { boundaryDragVisibleExtensionIdsRef, extensionDragRef, extensionLockRanges, extensionRanges, mode, sessionClockAt, setBoundaryConfirmed, setBoundaryPreview, setExtensionDrafts, setExtensionDrag, setExtensionLocks, setExtensionSourceExpanded, setSelectedExtensionId, setSelectedWindow, setSessionBoundary, setSyncState, visibleExtensionRanges } = context;
  const markSyncDirty = () => {
      setBoundaryConfirmed(false);
      setSyncState("dirty");
    };

  const selectExtensionCandidate = (candidate: ExtensionRange) => {
      const lock = extensionLockRanges[candidate.id];
      setSelectedExtensionId(candidate.id);
      setExtensionSourceExpanded(true);
      setSelectedWindow(`${lock.startClock.slice(0, 5)} - ${lock.endClock.slice(0, 5)}`);
    };

  const updateExtensionLock = (candidate: ExtensionRange, start: number, end: number) => {
      const minWindowSeconds = Math.min(4, Math.max(1, candidate.end - candidate.start));
      const nextStart = Number(clamp(start, candidate.start, Math.max(candidate.start, candidate.end - minWindowSeconds)).toFixed(1));
      const nextEnd = Number(clamp(end, nextStart + minWindowSeconds, candidate.end).toFixed(1));
      const startClock = sessionClockAt(nextStart);
      const endClock = sessionClockAt(nextEnd);

      setSelectedExtensionId(candidate.id);
      setExtensionLocks((current) => ({
        ...current,
        [candidate.id]: {
          startClock,
          endClock
        }
      }));
      setSelectedWindow(`${startClock.slice(0, 5)} - ${endClock.slice(0, 5)}`);
      markSyncDirty();
    };

  const secondsFromExtensionPointer = (candidate: ExtensionRange, clientX: number, rect: DOMRect) =>
      Number((candidate.start + clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1) * (candidate.end - candidate.start)).toFixed(1));

  const updateExtensionLockEdge = (candidate: ExtensionRange, edge: "start" | "end", value: number) => {
      const lock = extensionLockRanges[candidate.id];
      const minWindowSeconds = Math.min(4, Math.max(1, candidate.end - candidate.start));
      if (edge === "start") {
        updateExtensionLock(candidate, Math.min(value, lock.end - minWindowSeconds), lock.end);
        return;
      }
      updateExtensionLock(candidate, lock.start, Math.max(value, lock.start + minWindowSeconds));
    };

  const startExtensionRangeDrag = (candidate: ExtensionRange, event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      const currentLock = extensionLockRanges[candidate.id];
      const pointerStart = secondsFromExtensionPointer(candidate, event.clientX, rect);
      const dragTarget = (event.target as HTMLElement).closest("[data-extension-drag-mode]");
      const targetMode = dragTarget?.getAttribute("data-extension-drag-mode");
      const mode =
        targetMode === "start" || targetMode === "end" || targetMode === "window"
          ? targetMode
          : Math.abs(pointerStart - currentLock.start) <= Math.abs(pointerStart - currentLock.end)
            ? "start"
            : "end";

      event.currentTarget.setPointerCapture?.(event.pointerId);
      extensionDragRef.current = {
        candidateId: candidate.id,
        mode,
        pointerStart,
        lockStart: currentLock.start,
        lockEnd: currentLock.end
      };
      setExtensionDrag({ candidateId: candidate.id, mode });
      setSelectedExtensionId(candidate.id);
      setExtensionSourceExpanded(true);

      if (mode === "start" || mode === "end") {
        updateExtensionLockEdge(candidate, mode, pointerStart);
        return;
      }
      setSelectedWindow(`${currentLock.startClock.slice(0, 5)} - ${currentLock.endClock.slice(0, 5)}`);
    };

  const moveExtensionRangeDrag = (candidate: ExtensionRange, event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = extensionDragRef.current;
      if (!drag || drag.candidateId !== candidate.id) return;
      event.preventDefault();
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      const nextPointer = secondsFromExtensionPointer(candidate, event.clientX, rect);
      if (drag.mode === "window") {
        const duration = Math.max(1, drag.lockEnd - drag.lockStart);
        const nextStart = clamp(drag.lockStart + nextPointer - drag.pointerStart, candidate.start, candidate.end - duration);
        updateExtensionLock(candidate, nextStart, nextStart + duration);
        return;
      }
      updateExtensionLockEdge(candidate, drag.mode, nextPointer);
    };

  const finishExtensionRangeDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      extensionDragRef.current = null;
      setExtensionDrag(null);
    };

  const nudgeExtensionLock = (candidate: ExtensionRange, deltaSeconds: number) => {
      const lock = extensionLockRanges[candidate.id];
      const duration = lock.end - lock.start;
      const nextStart = clamp(lock.start + deltaSeconds, candidate.start, candidate.end - duration);
      updateExtensionLock(candidate, nextStart, nextStart + duration);
    };

  const getBoundaryPreviewWindow = (candidate: ExtensionRange, clip: BoundaryPreviewClip) => {
      const lock = extensionLockRanges[candidate.id];
      const previewPad = 6;
      if (clip === "before") {
        const end = lock.start;
        const start = Math.max(candidate.start, end - previewPad);
        return {
          start,
          end,
          label: "选区前段"
        };
      }
      if (clip === "after") {
        const start = lock.end;
        const end = Math.min(candidate.end, start + previewPad);
        return {
          start,
          end,
          label: "选区后段"
        };
      }
      return {
        start: lock.start,
        end: lock.end,
        label: "选区内容"
      };
    };

  const startBoundaryPreview = (candidate: ExtensionRange, clip: BoundaryPreviewClip, event?: ReactMouseEvent<HTMLButtonElement>) => {
      event?.stopPropagation();
      const window = getBoundaryPreviewWindow(candidate, clip);
      const windowText = `${sessionClockAt(window.start)} - ${sessionClockAt(window.end)}`;
      setSelectedExtensionId(candidate.id);
      setBoundaryPreview((current) => {
        const isSame = current?.kind === "extension" && current.id === candidate.id && current.clip === clip && current.playing;
        return {
          kind: "extension",
          id: candidate.id,
          clip,
          label: `${candidate.label} · ${window.label}`,
          windowText,
          playing: !isSame
        };
      });
      setSelectedWindow(`${sessionClockAt(window.start).slice(0, 5)} - ${sessionClockAt(window.end).slice(0, 5)}`);
    };

  const updateExtensionDecision = (candidate: BoundaryExtensionCandidate, decision: BoundaryExtensionDecision, event?: ReactMouseEvent<HTMLButtonElement>) => {
      event?.stopPropagation();
      setSelectedExtensionId(candidate.id);
      setExtensionDrafts((current) => ({ ...current, [candidate.id]: decision }));
      if (decision === "preview") {
        const candidateRange = extensionRanges.find((item) => item.id === candidate.id);
        if (candidateRange) startBoundaryPreview(candidateRange, "source");
        return;
      }
      if (decision === "merged") {
        const candidateRange = extensionRanges.find((item) => item.id === candidate.id);
        const lockedRange = extensionLockRanges[candidate.id];
        if (candidateRange && lockedRange) {
          setSessionBoundary((current) => {
            if (candidateRange.direction === "previous") return { ...current, start: Math.min(current.start, lockedRange.start) };
            return { ...current, end: Math.max(current.end, lockedRange.end) };
          });
          setSelectedWindow(`${lockedRange.startClock.slice(0, 5)} - ${lockedRange.endClock.slice(0, 5)}`);
        }
      }
      markSyncDirty();
    };

  const syncBoundaryExtensionOverlap = (edge: "start" | "end", nextValue: number) => {
      const dragVisibleIds = boundaryDragVisibleExtensionIdsRef.current;
      const matchedCandidates = visibleExtensionRanges.filter((candidate) => {
        if (dragVisibleIds && !dragVisibleIds.has(candidate.id)) return false;
        return (
          (edge === "start" && candidate.direction === "previous" && nextValue <= candidate.end) ||
          (edge === "end" && candidate.direction === "next" && nextValue >= candidate.start)
        );
      });
      setExtensionDrafts((current) => {
        let changed = false;
        const next = { ...current };
        matchedCandidates.forEach((candidate) => {
          if (next[candidate.id] !== "merged") {
            next[candidate.id] = "merged";
            changed = true;
          }
        });
        return changed ? next : current;
      });
    };

  return { ...context, markSyncDirty, selectExtensionCandidate, updateExtensionLock, secondsFromExtensionPointer, updateExtensionLockEdge, startExtensionRangeDrag, moveExtensionRangeDrag, finishExtensionRangeDrag, nudgeExtensionLock, getBoundaryPreviewWindow, startBoundaryPreview, updateExtensionDecision, syncBoundaryExtensionOverlap };
}

export type BoundaryExtensionActions = ReturnType<typeof createBoundaryExtensionActions>;
