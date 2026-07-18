import { stitchedWavSlices } from "../../../fixtures/boundaryFixtures";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";
import { Check, Headphones, SkipBack, SkipForward } from "lucide-react";

export function MinimapListeningStrip({ controller }: { controller: AnnotationMinimapController }) {
  const { boundaryAxisEnd, boundaryAxisStart, boundaryConfirmed, boundaryDrag, boundaryEndPct, boundaryStartPct, boundaryWindowWidth, confirmSessionBoundary, extensionDrafts, finishBoundaryDrag, mergedSliceCount, nudgeBoundary, openListeningMode, selectExtensionCandidate, selectedExtensionId, selectedSliceId, sessionBoundary, sessionRangeText, setSelectedSliceId, setSelectedWindow, sliceDecisions, startBoundaryDrag, stitchStripRef, stitchSummary, syncState, updateBoundaryFromPointer, visibleExtensionRanges } = controller;
  return (
    <div className="mm-listen-strip" aria-label="Minimap 当前窗口调听">
                <div className="mm-listen-meta">
                  <span>完整对话边界</span>
                  <strong>{sessionRangeText}</strong>
                  <em>
                    {stitchSummary} · {mergedSliceCount} 段合并 · {boundaryConfirmed ? "已确认" : "待确认"}
                  </em>
                </div>
                <div
                  ref={stitchStripRef}
                  className={boundaryDrag ? "mm-stitch-strip dragging" : "mm-stitch-strip"}
                  aria-label="完整会话来源片段与边界编辑"
                  onPointerMove={(event) => boundaryDrag && updateBoundaryFromPointer(event.clientX, boundaryDrag)}
                  onPointerUp={finishBoundaryDrag}
                  onPointerCancel={finishBoundaryDrag}
                >
                  <span
                    className="mm-boundary-window"
                    style={{ left: `${boundaryStartPct}%`, width: `${boundaryWindowWidth}%` }}
                    aria-hidden="true"
                  />
                  {visibleExtensionRanges
                    .filter((candidate) => candidate.direction === "previous")
                    .map((candidate) => {
                      const decision = extensionDrafts[candidate.id] ?? "idle";
                      return (
                        <button
                          key={candidate.id}
                          type="button"
                          className={["mm-stitch-piece", "extension", candidate.direction, decision, selectedExtensionId === candidate.id ? "selected" : ""].join(" ")}
                          style={{ flexGrow: candidate.end - candidate.start }}
                          onClick={() => selectExtensionCandidate(candidate)}
                            title={`${candidate.label} ${candidate.wallStart}-${candidate.wallEnd} · 点击锁定段中窗口`}
                          >
                            <b>{candidate.label.replace("段", "")}</b>
                            <em>{candidate.wallStart.slice(3, 8)}-{candidate.wallEnd.slice(3, 8)}</em>
                            <small>{decision === "merged" ? "已锁定" : "可锁定"}</small>
                        </button>
                      );
                    })}
                  {stitchedWavSlices.map((slice, index) => {
                    const decision = sliceDecisions[slice.id] ?? "merged";
                    const range = `${slice.sourceStart}s-${slice.sourceEnd}s`;
                    return (
                      <button
                        key={slice.id}
                        type="button"
                        className={[
                          "mm-stitch-piece",
                          index === 0 ? "active" : "",
                          decision,
                          selectedSliceId === slice.id ? "selected" : ""
                        ].join(" ")}
                        style={{ flexGrow: slice.conversationEnd - slice.conversationStart }}
                        onClick={() => {
                          setSelectedSliceId(slice.id);
                          setSelectedWindow(`${slice.wallStart.slice(0, 5)} - ${slice.wallEnd.slice(0, 5)}`);
                        }}
                        title={`${slice.label} ${range} · 点击查看切分/合并原因`}
                      >
                        <b>{slice.label}</b>
                        <em>{range}</em>
                        <small>{decision === "merged" ? "并入" : "拆出"}</small>
                      </button>
                    );
                  })}
                  {visibleExtensionRanges
                    .filter((candidate) => candidate.direction === "next")
                    .map((candidate) => {
                      const decision = extensionDrafts[candidate.id] ?? "idle";
                      return (
                        <button
                          key={candidate.id}
                          type="button"
                          className={["mm-stitch-piece", "extension", candidate.direction, decision, selectedExtensionId === candidate.id ? "selected" : ""].join(" ")}
                          style={{ flexGrow: candidate.end - candidate.start }}
                          onClick={() => selectExtensionCandidate(candidate)}
                            title={`${candidate.label} ${candidate.wallStart}-${candidate.wallEnd} · 点击锁定段中窗口`}
                          >
                            <b>{candidate.label.replace("段", "")}</b>
                            <em>{candidate.wallStart.slice(3, 8)}-{candidate.wallEnd.slice(3, 8)}</em>
                            <small>{decision === "merged" ? "已锁定" : "可锁定"}</small>
                        </button>
                      );
                    })}
                  <span
                    className={boundaryDrag === "start" ? "mm-boundary-handle start dragging" : "mm-boundary-handle start"}
                    style={{ left: `${boundaryStartPct}%` }}
                    role="slider"
                    tabIndex={0}
                    aria-label="完整对话开始边界"
                    aria-valuemin={boundaryAxisStart}
                    aria-valuemax={boundaryAxisEnd}
                    aria-valuenow={sessionBoundary.start}
                    onPointerDown={(event) => startBoundaryDrag(event, "start")}
                    onKeyDown={(event) => {
                      if (event.key === "ArrowLeft") nudgeBoundary("start", -5);
                      if (event.key === "ArrowRight") nudgeBoundary("start", 5);
                    }}
                  />
                  <span
                    className={boundaryDrag === "end" ? "mm-boundary-handle end dragging" : "mm-boundary-handle end"}
                    style={{ left: `${boundaryEndPct}%` }}
                    role="slider"
                    tabIndex={0}
                    aria-label="完整对话结束边界"
                    aria-valuemin={boundaryAxisStart}
                    aria-valuemax={boundaryAxisEnd}
                    aria-valuenow={sessionBoundary.end}
                    onPointerDown={(event) => startBoundaryDrag(event, "end")}
                    onKeyDown={(event) => {
                      if (event.key === "ArrowLeft") nudgeBoundary("end", -5);
                      if (event.key === "ArrowRight") nudgeBoundary("end", 5);
                    }}
                  />
                  <i className="mm-stitch-cursor" />
                </div>
                <div className="mm-listen-actions">
                  <button onClick={() => setSelectedWindow("12:18 - 12:29")} title="回听前一个窗口">
                    <SkipBack size={11} />
                    5s
                  </button>
                  <button onClick={() => setSelectedWindow("12:23 - 12:33")} title="回到当前证据窗口">
                    <Headphones size={12} />
                    定位
                  </button>
                  <button onClick={() => setSelectedWindow("12:34 - 12:42")} title="试听后一个窗口">
                    5s
                    <SkipForward size={11} />
                  </button>
                  <button className="open-listening" onClick={openListeningMode}>
                    完整调听
                  </button>
                  <button
                    className={boundaryConfirmed ? "confirm-boundary done" : "confirm-boundary"}
                    onClick={() => void confirmSessionBoundary()}
                    disabled={syncState === "saving"}
                  >
                    <Check size={11} />
                    {syncState === "saving" ? "保存中" : boundaryConfirmed ? "已保存" : "保存边界"}
                  </button>
                </div>
              </div>
  );
}
