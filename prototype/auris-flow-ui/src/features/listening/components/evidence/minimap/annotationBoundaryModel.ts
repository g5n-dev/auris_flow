import { clamp } from "../../../../../shared/runtime/math";
import { boundaryExtensionCandidates, stitchedWavSlices } from "../../../fixtures/boundaryFixtures";
import type { BoundaryExtensionLock } from "../../../fixtures/boundaryFixtures";
import { clockToSeconds, secondsToClock } from "../../../model/listeningTime";
import type { AnnotationAssociationModel } from "./annotationAssociationModel";
import type { AnnotationMinimapProps } from "./annotationMinimapTypes";

export function buildAnnotationBoundaryModel(context: AnnotationAssociationModel) {
  const { extensionDrafts, extensionLocks, selectedExtensionId, selectedSliceId, sessionBoundary, sliceDecisions, syncState } = context;
  const conversationDuration = stitchedWavSlices[stitchedWavSlices.length - 1]?.conversationEnd ?? 1;

  const sessionBaseSeconds = clockToSeconds(stitchedWavSlices[0]?.wallStart ?? "00:00:00");

  const extensionRanges = boundaryExtensionCandidates.map((candidate) => ({
      ...candidate,
      start: clockToSeconds(candidate.wallStart) - sessionBaseSeconds,
      end: clockToSeconds(candidate.wallEnd) - sessionBaseSeconds
    }));

  const extensionLockRanges = Object.fromEntries(
      extensionRanges.map((candidate) => {
        const lock = extensionLocks[candidate.id] ?? {
          startClock: candidate.previewStart,
          endClock: candidate.previewEnd
        };
        const minWindowSeconds = Math.min(4, Math.max(1, candidate.end - candidate.start));
        const rawStart = clockToSeconds(lock.startClock) - sessionBaseSeconds;
        const maxStart = Math.max(candidate.start, candidate.end - minWindowSeconds);
        const start = Number(clamp(rawStart, candidate.start, maxStart).toFixed(1));
        const rawEnd = clockToSeconds(lock.endClock) - sessionBaseSeconds;
        const end = Number(clamp(rawEnd, start + minWindowSeconds, candidate.end).toFixed(1));

        return [
          candidate.id,
          {
            start,
            end,
            startClock: secondsToClock(sessionBaseSeconds + start),
            endClock: secondsToClock(sessionBaseSeconds + end)
          }
        ];
      })
    ) as Record<string, BoundaryExtensionLock & { start: number; end: number }>;

  const previousExtensionRanges = extensionRanges.filter((candidate) => candidate.direction === "previous").sort((a, b) => b.end - a.end);

  const nextExtensionRanges = extensionRanges.filter((candidate) => candidate.direction === "next").sort((a, b) => a.start - b.start);

  const extensionIsMerged = (candidate: { id: string }) => extensionDrafts[candidate.id] === "merged";

  const visiblePreviousExtensionRanges = previousExtensionRanges.filter(
      (_candidate, index) => index === 0 || previousExtensionRanges.slice(0, index).every(extensionIsMerged)
    );

  const visibleNextExtensionRanges = nextExtensionRanges.filter(
      (_candidate, index) => index === 0 || nextExtensionRanges.slice(0, index).every(extensionIsMerged)
    );

  const visibleExtensionRanges = [...visiblePreviousExtensionRanges, ...visibleNextExtensionRanges];

  const boundaryAxisStart = Math.min(sessionBoundary.start, 0, ...visiblePreviousExtensionRanges.map((candidate) => candidate.start));

  const boundaryAxisEnd = Math.max(sessionBoundary.end, conversationDuration, ...visibleNextExtensionRanges.map((candidate) => candidate.end));

  const boundaryAxisDuration = Math.max(1, boundaryAxisEnd - boundaryAxisStart);

  const boundaryPct = (seconds: number) => ((seconds - boundaryAxisStart) / boundaryAxisDuration) * 100;

  const selectedSlice = stitchedWavSlices.find((slice) => slice.id === selectedSliceId) ?? null;

  const mergedSliceCount = Object.values(sliceDecisions).filter((status) => status === "merged").length;

  const mergedExtensionCount = Object.values(extensionDrafts).filter((status) => status === "merged").length;

  const previewExtensionCount = Object.values(extensionDrafts).filter((status) => status === "preview").length;

  const mergedExtensionLabels = extensionRanges
      .filter((candidate) => extensionDrafts[candidate.id] === "merged")
      .map((candidate) => candidate.label)
      .join(" / ");

  const boundaryStartPct = boundaryPct(sessionBoundary.start);

  const boundaryEndPct = boundaryPct(sessionBoundary.end);

  const boundaryWindowWidth = Math.max(1, boundaryEndPct - boundaryStartPct);

  const sessionClockAt = (offsetSeconds: number) => secondsToClock(sessionBaseSeconds + offsetSeconds);

  const sessionRangeText = `${sessionClockAt(sessionBoundary.start)} - ${sessionClockAt(sessionBoundary.end)}`;

  const stitchSummary = stitchedWavSlices
      .map((slice) => `${slice.label}:${slice.sourceStart}-${slice.sourceEnd}s`)
      .join(" → ");

  const selectedSourceShape = selectedSlice ? `当前取用 ${selectedSlice.sourceEnd - selectedSlice.sourceStart}s / chunk 可变` : "chunk 可变";

  const syncStateMeta = {
      synced: { label: "已保存", detail: "完整对话开始/结束已记录" },
      dirty: { label: "待保存", detail: "完整对话开始/结束有草稿改动" },
      saving: { label: "保存中", detail: "正在写入 ConversationBoundary" },
      error: { label: "保存失败", detail: "后端未确认，当前仍是草稿" }
    }[syncState];

  const conversationBoundaryTicks = Array.from(new Set([Math.round(boundaryAxisStart), 0, 120, 240, 360, 480, 600, Math.round(conversationDuration), Math.round(boundaryAxisEnd)])).filter(
      (tick) => tick >= boundaryAxisStart && tick <= boundaryAxisEnd
    );

  const conversationOverlapZones = stitchedWavSlices.slice(1).map((slice) => {
      const center = slice.conversationStart;
      const width = Math.min(16, Math.max(8, conversationDuration * 0.018));
      const start = clamp(center - width / 2, 0, conversationDuration);
      const end = clamp(center + width / 2, start, conversationDuration);
      return {
        key: `${slice.id}-overlap`,
        label: `${stitchedWavSlices.find((item) => item.conversationEnd === slice.conversationStart)?.label ?? ""}/${slice.label} overlap`,
        start,
        end
      };
    });

  const selectedExtension = selectedExtensionId ? extensionRanges.find((candidate) => candidate.id === selectedExtensionId) ?? null : null;

  const selectedExtensionLock = selectedExtensionId ? extensionLockRanges[selectedExtensionId] ?? null : null;

  const boundaryImpactRows = [
      ["完整对话窗口", sessionRangeText, "唯一可编辑对象"],
      ["来源分片拼接", `${mergedSliceCount}/${stitchedWavSlices.length} 段并入`, "当前可见来源证据"],
      [
        "前后来源扩展",
        `${mergedExtensionCount} 段待并入 / ${previewExtensionCount} 段试听`,
        selectedExtension && selectedExtensionLock
          ? `${selectedExtension.label} 选区 ${selectedExtensionLock.startClock} - ${selectedExtensionLock.endClock}`
          : mergedExtensionLabels || "拖动候选源左右手柄，自由选定并入范围"
      ],
      ["下游时间索引", "跟随边界自动重建", "不可在此单独调偏移"],
      ["业务资产状态", "跟随边界重新裁剪", "保存后同步下游资产状态"]
    ];

  return { ...context, conversationDuration, sessionBaseSeconds, extensionRanges, extensionLockRanges, previousExtensionRanges, nextExtensionRanges, extensionIsMerged, visiblePreviousExtensionRanges, visibleNextExtensionRanges, visibleExtensionRanges, boundaryAxisStart, boundaryAxisEnd, boundaryAxisDuration, boundaryPct, selectedSlice, mergedSliceCount, mergedExtensionCount, previewExtensionCount, mergedExtensionLabels, boundaryStartPct, boundaryEndPct, boundaryWindowWidth, sessionClockAt, sessionRangeText, stitchSummary, selectedSourceShape, syncStateMeta, conversationBoundaryTicks, conversationOverlapZones, selectedExtension, selectedExtensionLock, boundaryImpactRows };
}

export type AnnotationBoundaryModel = ReturnType<typeof buildAnnotationBoundaryModel>;
