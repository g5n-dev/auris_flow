import { stitchedWavSlices } from "../../../fixtures/boundaryFixtures";
import type { AnnotationMinimapController } from "./conversationBoundaryActions";

export function BoundaryTuneCard({ controller }: { controller: AnnotationMinimapController }) {
  const { boundaryAxisDuration, boundaryAxisEnd, boundaryAxisStart, boundaryDrag, boundaryEndPct, boundaryId, boundaryImpactRows, boundaryPct, boundaryStartPct, boundaryWindowWidth, confirmSessionBoundary, conversationBoundaryTicks, conversationOverlapZones, extensionDrafts, finishBoundaryDrag, modalBoundaryStripRef, moveNearestModalBoundary, nudgeSessionBoundary, resetConversationBoundary, selectExtensionCandidate, selectedExtensionId, selectedSlice, sessionBoundary, sessionClockAt, sessionRangeText, setSelectedSliceId, sliceDecisions, startModalBoundaryDrag, syncState, syncStateMeta, updateModalBoundaryFromPointer, visibleExtensionRanges } = controller;
  return (
    <section className="session-boundary-card boundary-tune-card">
                      <div className="boundary-tune-head">
                        <div>
                          <span>完整对话边界精调</span>
                          <strong>{sessionRangeText}</strong>
                          <p>拖动手柄按 0.1s 连续移动；进入候选 wav 只标记并入，不自动吸附到候选段默认点。</p>
                        </div>
                        <b className={`sync-status-pill ${syncState}`}>{syncStateMeta.label}</b>
                      </div>
                      <div className="conversation-boundary-editor">
                        <div className="conversation-boundary-main">
                          <div className="conversation-boundary-ruler" aria-hidden="true">
                            {conversationBoundaryTicks.map((tick) => (
                              <span key={tick} style={{ left: `${boundaryPct(tick)}%` }}>
                                {tick}s
                              </span>
                            ))}
                          </div>
                          <div
                            ref={modalBoundaryStripRef}
                            className={boundaryDrag ? "conversation-boundary-strip dragging" : "conversation-boundary-strip"}
                            onPointerDown={(event) => {
                              if (event.target === event.currentTarget) moveNearestModalBoundary(event);
                            }}
                            onPointerMove={(event) => boundaryDrag && updateModalBoundaryFromPointer(event.clientX, boundaryDrag)}
                            onPointerUp={finishBoundaryDrag}
                            onPointerCancel={finishBoundaryDrag}
                          >
                            <span
                              className="conversation-boundary-window"
                              style={{ left: `${boundaryStartPct}%`, width: `${boundaryWindowWidth}%` }}
                            />
                            {conversationOverlapZones.map((zone) => (
                                <span
                                  key={zone.key}
                                  className="conversation-overlap-zone"
                                  style={{
                                    left: `${boundaryPct(zone.start)}%`,
                                    width: `${Math.max(0.8, ((zone.end - zone.start) / boundaryAxisDuration) * 100)}%`
                                  }}
                                >
                                  <em>{zone.label}</em>
                                </span>
                              ))}
                            {visibleExtensionRanges.map((candidate) => {
                              const decision = extensionDrafts[candidate.id] ?? "idle";
                              const leftPct = boundaryPct(candidate.start);
                              const widthPct = ((candidate.end - candidate.start) / boundaryAxisDuration) * 100;
                              return (
                                <button
                                  key={candidate.id}
                                  type="button"
                                  className={[
                                    "conversation-fragment",
                                    "extension",
                                    candidate.direction,
                                    decision,
                                    selectedExtensionId === candidate.id ? "selected" : ""
                                  ].join(" ")}
                                  style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                                  onClick={() => selectExtensionCandidate(candidate)}
                                  title={`${candidate.label} ${candidate.wallStart}-${candidate.wallEnd} · 点击锁定段中窗口`}
                                >
                                  <b>{candidate.label}</b>
                                  <span>{decision === "merged" ? "已锁定" : "可锁定"}</span>
                                </button>
                              );
                            })}
                              {stitchedWavSlices.map((slice) => {
                                const decision = sliceDecisions[slice.id] ?? "merged";
                                const leftPct = boundaryPct(slice.conversationStart);
                                const widthPct = ((slice.conversationEnd - slice.conversationStart) / boundaryAxisDuration) * 100;
                              return (
                                <button
                                  key={slice.id}
                                  type="button"
                                  className={[
                                    "conversation-fragment",
                                    decision,
                                    selectedSlice!.id === slice.id ? "selected" : ""
                                  ].join(" ")}
                                  style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                                  onClick={() => setSelectedSliceId(slice.id)}
                                >
                                  <b>{slice.label}</b>
                                  <span>{slice.wallStart.slice(0, 5)}-{slice.wallEnd.slice(0, 5)}</span>
                                </button>
                              );
                            })}
                            <span
                              className={boundaryDrag === "start" ? "conversation-boundary-handle start dragging" : "conversation-boundary-handle start"}
                              style={{ left: `${boundaryStartPct}%` }}
                              role="slider"
                              tabIndex={0}
                                aria-label="拖动完整对话开始边界"
                                aria-valuemin={boundaryAxisStart}
                                aria-valuemax={boundaryAxisEnd}
                              aria-valuenow={sessionBoundary.start}
                              onPointerDown={(event) => startModalBoundaryDrag(event, "start")}
                              onKeyDown={(event) => {
                                if (event.key === "ArrowLeft") nudgeSessionBoundary("start", -5);
                                if (event.key === "ArrowRight") nudgeSessionBoundary("start", 5);
                              }}
                            />
                            <span
                              className={boundaryDrag === "end" ? "conversation-boundary-handle end dragging" : "conversation-boundary-handle end"}
                              style={{ left: `${boundaryEndPct}%` }}
                              role="slider"
                              tabIndex={0}
                                aria-label="拖动完整对话结束边界"
                                aria-valuemin={boundaryAxisStart}
                                aria-valuemax={boundaryAxisEnd}
                              aria-valuenow={sessionBoundary.end}
                              onPointerDown={(event) => startModalBoundaryDrag(event, "end")}
                              onKeyDown={(event) => {
                                if (event.key === "ArrowLeft") nudgeSessionBoundary("end", -5);
                                if (event.key === "ArrowRight") nudgeSessionBoundary("end", 5);
                              }}
                            />
                          </div>
                          <div className="conversation-fragment-list">
                            {stitchedWavSlices.map((slice) => {
                              const decision = sliceDecisions[slice.id] ?? "merged";
                              const isSelected = selectedSlice!.id === slice.id;
                              return (
                                <button
                                  key={slice.id}
                                  type="button"
                                  className={isSelected ? "active" : ""}
                                  onClick={() => setSelectedSliceId(slice.id)}
                                >
                                  <strong>{slice.label}</strong>
                                  <span>{slice.sourceStart}s-{slice.sourceEnd}s</span>
                                  <em>{decision === "merged" ? "并入完整对话" : "拆出新会话"}</em>
                                </button>
                              );
                            })}
                          </div>
                        </div>
                        <aside className="boundary-tune-controls" aria-label="完整对话边界精调控制">
                          <div className="boundary-value-row">
                            <span>开始</span>
                            <strong>{sessionBoundary.start.toFixed(1)}s</strong>
                            <em>{sessionClockAt(sessionBoundary.start)}</em>
                          </div>
                          <div className="boundary-value-row">
                            <span>结束</span>
                            <strong>{sessionBoundary.end.toFixed(1)}s</strong>
                            <em>{sessionClockAt(sessionBoundary.end)}</em>
                          </div>
                            <div className="boundary-sync-note">
                              <span>保存记录</span>
                              <p>拖动左侧蓝色开始手柄和橙色结束手柄后，直接保存当前完整对话的开始/结束；下游时间索引和资产状态由系统自动重建。</p>
                            </div>
                          <button
                            type="button"
                            className="sync-primary"
                            onClick={() => void confirmSessionBoundary()}
                            disabled={!boundaryId || syncState === "saving"}
                            title={!boundaryId ? "当前 HumanReviewTask 未绑定可修订的会话边界。" : "加入当前人审决定，提交后统一写入并回读。"}
                          >
                            {syncState === "saving" ? "暂存中" : "加入边界修订"}
                          </button>
                          <button type="button" onClick={resetConversationBoundary}>
                            恢复系统建议边界
                          </button>
                        </aside>
                      </div>
                        <div className="boundary-impact-list">
                          {boundaryImpactRows.map(([name, value, detail]) => (
                            <div key={name} className="boundary-impact-row">
                              <span>{name}</span>
                              <strong>{value}</strong>
                              <em>{detail}</em>
                            </div>
                          ))}
                        </div>
                      </section>
  );
}
